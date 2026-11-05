"""Optional, admin-only media validation + managed STRM exports.

No scraping, authentication evasion, or content proxying. The caller must supply
playlists containing sources they are authorized to access. Only managed files
under EXPORT_ROOT are moved on recurring failures.
"""
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from indexer import DB
from catalog_identity import key_for, from_managed, compatible

DATA = Path(os.environ.get('INDEXER_STATE_DIR', '/app/data')).resolve()
STATE_DB = DATA / 'managed.sqlite'
EXPORT_ROOT = Path(os.environ.get('INDEXER_EXPORT_DIR', '/app/export')).resolve()
BASE_PLAY_URL = os.environ.get('INDEXER_PLAY_BASE', 'http://127.0.0.1:8794').rstrip('/')
FAIL_LIMIT = max(2, min(10, int(os.environ.get('INDEXER_FAILURE_LIMIT', '3'))))
CHECK_INTERVAL = max(3600, int(os.environ.get('INDEXER_CHECK_INTERVAL', '86400')))
NOTIFY_WEBHOOK = os.environ.get('INDEXER_NOTIFY_WEBHOOK', '').strip()
NOTIFY_TOKEN = os.environ.get('INDEXER_NOTIFY_TOKEN', '').strip()
ALLOWED_HOSTS = {h.strip().casefold() for h in os.environ.get('INDEXER_ALLOWED_MEDIA_HOSTS', '').split(',') if h.strip()}
LOCK = threading.RLock()
PREVIEWS = {}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def connect():
    c = sqlite3.connect(STATE_DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA busy_timeout=15000')
    return c


def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript('''
            CREATE TABLE IF NOT EXISTS managed (
                key TEXT PRIMARY KEY, entry_id INTEGER NOT NULL,
                identity TEXT NOT NULL, title TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', original_title TEXT NOT NULL DEFAULT '',
                relative_path TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                failures INTEGER NOT NULL DEFAULT 0,
                validated_at TEXT, checked_at REAL,
                next_check REAL NOT NULL DEFAULT 0,
                last_error TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS validations (
                entry_id INTEGER PRIMARY KEY, identity TEXT NOT NULL,
                ok INTEGER NOT NULL, checked_at REAL NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL, title TEXT NOT NULL,
                event TEXT NOT NULL, message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_managed_due ON managed(next_check);
        ''')
        # Upgrade existing user databases in place; keep historical entries.
        columns = {r[1] for r in c.execute('PRAGMA table_info(managed)')}
        if 'content_key' not in columns:
            c.execute('ALTER TABLE managed ADD COLUMN content_key TEXT')
        for row in c.execute('SELECT * FROM managed WHERE content_key IS NULL').fetchall():
            c.execute('UPDATE managed SET content_key=? WHERE key=?', (from_managed(row), row['key']))
        c.execute('CREATE INDEX IF NOT EXISTS idx_managed_content ON managed(content_key)')


def _public_host(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.fragment:
        raise ValueError('URL de mídia inválida')
    if len(url) > 4096:
        raise ValueError('URL muito longa')
    host = p.hostname.casefold().rstrip('.')
    if ALLOWED_HOSTS and host not in ALLOWED_HOSTS:
        raise ValueError('Domínio não autorizado nas configurações')
    if host in ('localhost', 'localhost.localdomain') or host.endswith('.localhost') or host.endswith('.local'):
        raise ValueError('Destino local bloqueado')
    try:
        addresses = socket.getaddrinfo(host, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError('Não foi possível resolver o servidor')
    if not addresses:
        raise ValueError('Servidor sem endereço')
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError('Endereços internos não são permitidos')
    return host


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = build_opener(SafeRedirect())


def get_bytes(url, length=1024):
    _public_host(url)
    req = Request(url, headers={'Range': f'bytes=0-{length-1}', 'User-Agent': 'STRMIndexer/0.3', 'Accept': '*/*'}, method='GET')
    with OPENER.open(req, timeout=12) as res:
        if res.status not in (200, 206):
            raise ValueError('HTTP inesperado')
        data = res.read(length)
        return res.status, res.headers.get('Content-Type', '').split(';')[0].strip().lower(), data, res.geturl()


def _hls_segment(url, depth=0):
    if depth > 2:
        raise ValueError('Playlist HLS muito aninhada')
    _, typ, content, final = get_bytes(url, 65536)
    if not content.lstrip().startswith(b'#EXTM3U'):
        raise ValueError('Playlist HLS inválida')
    lines = content.decode('utf-8-sig', errors='replace').splitlines()
    references = [x.strip() for x in lines if x.strip() and not x.strip().startswith('#')]
    if not references:
        raise ValueError('Playlist sem mídia')
    if any(x.startswith('#EXT-X-KEY:') for x in lines):
        raise ValueError('Playlist criptografada: acesso externo não verificado')
    ref = urljoin(final, references[0])
    if any(x.startswith('#EXT-X-STREAM-INF:') for x in lines):
        return _hls_segment(ref, depth+1)
    _, typ, data, _ = get_bytes(ref, 1024)
    if not data or typ.startswith('text/html') or b'<html' in data[:160].lower():
        raise ValueError('Segmento não acessível como mídia')
    return 'HLS: playlist e primeiro segmento acessíveis'


def validate_media(url):
    try:
        status, typ, content, final = get_bytes(url, 1024)
        path = urlsplit(final).path.lower()
        if content.startswith(b'#EXTM3U') or typ in ('application/vnd.apple.mpegurl', 'application/x-mpegurl') or path.endswith('.m3u8'):
            return True, _hls_segment(final)
        if typ in ('video/mp4', 'video/webm', 'video/x-matroska', 'video/mp2t', 'application/octet-stream') or path.endswith(('.mp4', '.mkv', '.ts', '.webm')):
            if not content or content[:160].lstrip().lower().startswith((b'<html', b'<!doctype')):
                raise ValueError('Resposta não contém mídia')
            return True, 'HTTP %s · %s · primeiros bytes recebidos' % (status, typ or 'binário')
        return False, 'Tipo não reconhecido: ' + (typ or 'desconhecido')
    except HTTPError as e:
        return False, 'HTTP ' + str(e.code)
    except (URLError, ValueError, OSError, TimeoutError) as e:
        return False, str(e)[:150]


def entry(entry_id):
    if not isinstance(entry_id, int) or not 1 <= entry_id <= 2147483647:
        raise ValueError('ID inválido')
    with sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=12) as c:
        c.row_factory = sqlite3.Row
        result = c.execute('SELECT * FROM entries WHERE id=?', (entry_id,)).fetchone()
    if not result:
        raise ValueError('Entrada ausente do catálogo')
    return dict(result)


def identity(e):
    raw = '\0'.join(str(e.get(k) or '') for k in ('source', 'original_title', 'kind', 'season', 'episode', 'media_url'))
    return hashlib.sha256(raw.encode()).hexdigest()


def check_entry(entry_id, force=False):
    e = entry(entry_id)
    ident = identity(e)
    with LOCK:
        with connect() as c:
            prev = c.execute('SELECT * FROM validations WHERE entry_id=?', (entry_id,)).fetchone()
        if not force and prev and prev['identity'] == ident and time.time()-prev['checked_at'] < 600:
            return {'id': entry_id, 'ok': bool(prev['ok']), 'reason': prev['reason'], 'cached': True}
        good, reason = validate_media(e['media_url'])
        with connect() as c:
            c.execute('INSERT OR REPLACE INTO validations VALUES (?,?,?,?,?)', (entry_id, ident, int(good), time.time(), reason))
        return {'id': entry_id, 'ok': good, 'reason': reason, 'cached': False}


def clean_name(s):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', s or '').strip(' .')
    return s[:110] or 'Sem título'


def file_for(e, key):
    title = clean_name(e['title'])
    if e['kind'] == 'movie':
        label = f'{title} ({e["year"]})' if e['year'] else title
        return str(Path('Filmes') / (clean_name(label) + '-' + key[:10] + '.strm'))
    if e['kind'] == 'episode' and e['season'] is not None and e['episode'] is not None:
        season, episode = int(e['season']), int(e['episode'])
        return str(Path('Series') / title / f'Season {season:02}' / f'{title} S{season:02}E{episode:02}-{key[:10]}.strm')
    raise ValueError('O item não é filme nem episódio identificado')


def safe_dest(relative):
    target = (EXPORT_ROOT / relative).resolve()
    if not target.is_relative_to(EXPORT_ROOT):
        raise ValueError('Caminho de exportação inválido')
    return target


def _atomic_strm(path, key):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + secrets.token_hex(5) + '.tmp')
    temp.write_text(f'{BASE_PLAY_URL}/play/{key}\n', encoding='utf-8')
    os.replace(temp, path)


def add_entry(entry_id):
    with LOCK:
        e = entry(entry_id)
        ident = identity(e)
        content = key_for(e['kind'], e['title'], e.get('year'), e.get('season'), e.get('episode'))
        with connect() as c:
            existing = c.execute('SELECT * FROM managed WHERE kind=?', (e['kind'],)).fetchall()
            already = next((r for r in existing if r['identity'] == ident or compatible(content, r['content_key'])), None)
            if already:
                return {'key': already['key'], 'status': already['status'], 'already_added': True, 'path': already['relative_path'], 'same_content': True}
            check = check_entry(entry_id)
            if not check['ok']:
                raise ValueError('Fonte não validada: ' + check['reason'])
            key = secrets.token_urlsafe(24)
            rel = file_for(e, key)
            target = safe_dest(rel)
            if target.exists():
                raise ValueError('Arquivo já existe')
            _atomic_strm(target, key)
            try:
                c.execute('''INSERT INTO managed
                    (key,entry_id,identity,title,kind,source,original_title,relative_path,status,failures,validated_at,checked_at,next_check,last_error,created_at,content_key)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (key, entry_id, ident, e['title'], e['kind'], e['source'], e['original_title'], rel, 'active', 0, utc_now(), time.time(), time.time()+CHECK_INTERVAL, None, utc_now(), content))
            except Exception:
                target.unlink(missing_ok=True)
                raise
        return {'key': key, 'status': 'active', 'already_added': False, 'path': rel}


def watched():
    with connect() as c:
        rows = c.execute('''SELECT key,title,kind,relative_path,status,failures,validated_at,
                            last_error,created_at FROM managed ORDER BY created_at DESC LIMIT 1000''').fetchall()
        alerts = c.execute('SELECT * FROM alerts ORDER BY id DESC LIMIT 50').fetchall()
    return {'items': [dict(row) for row in rows], 'alerts': [dict(row) for row in alerts]}


def send_notification(event, title, message):
    # Optional external webhook: metadata only; never include playlist media URLs.
    if not NOTIFY_WEBHOOK:
        return
    try:
        _public_host(NOTIFY_WEBHOOK)
        if not NOTIFY_WEBHOOK.startswith('https://'):
            raise ValueError('O webhook deve usar HTTPS')
        payload = json.dumps({'event': event, 'title': title, 'message': message, 'at': utc_now()}, ensure_ascii=False).encode()
        headers = {'Content-Type': 'application/json'}
        if NOTIFY_TOKEN:
            headers['Authorization'] = 'Bearer ' + NOTIFY_TOKEN
        req = Request(NOTIFY_WEBHOOK, data=payload, method='POST', headers=headers)
        with OPENER.open(req, timeout=5) as response:
            response.read(128)
    except Exception as exc:
        print('NOTIFY falhou:', type(exc).__name__, flush=True)


def record_alert(c, row, event, message):
    c.execute('INSERT INTO alerts (key,title,event,message,created_at) VALUES (?,?,?,?,?)',
              (row['key'], row['title'], event, message, utc_now()))


def current_entry(row):
    try:
        e = entry(row['entry_id'])
        if identity(e) == row['identity']:
            return e
    except ValueError:
        pass
    # Numeric IDs can change across imports. Match source and original title,
    # and confirm the entire URL fingerprint before trusting a new entry.
    with sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=10) as c:
        c.row_factory = sqlite3.Row
        candidates = c.execute('SELECT * FROM entries WHERE source=? AND original_title=? AND kind=?',
                               (row['source'], row['original_title'], row['kind']))
        for candidate in candidates:
            possible = dict(candidate)
            if identity(possible) == row['identity']:
                with connect() as state:
                    state.execute('UPDATE managed SET entry_id=? WHERE key=?', (possible['id'], row['key']))
                return possible
    return None


def recheck(key):
    with LOCK:
        with connect() as c:
            row = c.execute('SELECT * FROM managed WHERE key=?', (key,)).fetchone()
        if not row:
            raise ValueError('Conteúdo não gerenciado')
        e = current_entry(row)
        good, reason = validate_media(e['media_url']) if e else (False, 'Entrada não encontrada após reimportação')
        failures = 0 if good else row['failures'] + 1
        next_status = 'active' if good else ('quarantined' if failures >= FAIL_LIMIT else row['status'])
        target = safe_dest(row['relative_path'])
        quarantine = (DATA / 'quarantine' / (row['key'] + '.strm')).resolve()
        if not quarantine.is_relative_to(DATA):
            raise ValueError('Caminho de quarentena inválido')
        event = None
        with connect() as c:
            if next_status == 'quarantined' and row['status'] != 'quarantined':
                event = 'unavailable'
                quarantine.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    shutil.move(str(target), str(quarantine))
                if target.with_suffix('.nfo').exists():
                    shutil.move(str(target.with_suffix('.nfo')), str(quarantine.with_suffix('.nfo')))
                record_alert(c, row, 'unavailable', 'Fonte removida da pasta gerenciada após falhas consecutivas')
            elif good and row['status'] == 'quarantined':
                event = 'restored'
                target.parent.mkdir(parents=True, exist_ok=True)
                if quarantine.exists():
                    shutil.move(str(quarantine), str(target))
                else:
                    _atomic_strm(target, row['key'])
                if quarantine.with_suffix('.nfo').exists():
                    shutil.move(str(quarantine.with_suffix('.nfo')), str(target.with_suffix('.nfo')))
                record_alert(c, row, 'restored', 'Fonte recuperada; STRM restaurado na pasta gerenciada')
            c.execute('''UPDATE managed SET status=?, failures=?, last_error=?, validated_at=?, checked_at=?, next_check=?
                         WHERE key=?''', (next_status, failures, None if good else reason, utc_now() if good else row['validated_at'],
                                        time.time(), time.time()+CHECK_INTERVAL, key))
        if event:
            send_notification(event, row['title'], 'Fonte indisponível: STRM colocado em quarentena' if event == 'unavailable' else 'Fonte recuperada: STRM restaurado')
        return {'key': key, 'ok': good, 'status': next_status, 'failures': failures,
                'required_failures': FAIL_LIMIT, 'reason': reason}


def get_play_url(key):
    with connect() as c:
        row = c.execute('SELECT * FROM managed WHERE key=? AND status=?', (key, 'active')).fetchone()
    if not row:
        return None
    e = current_entry(row)
    return e['media_url'] if e else None


def make_preview(entry_id):
    validation = check_entry(entry_id)
    if not validation['ok']:
        return validation
    ticket = secrets.token_urlsafe(28)
    with LOCK:
        now = time.time()
        for t, (_, expires) in list(PREVIEWS.items()):
            if expires < now:
                PREVIEWS.pop(t, None)
        PREVIEWS[ticket] = (entry_id, now + 300)
    return {**validation, 'preview': '/preview/' + ticket}


def preview_url(ticket):
    with LOCK:
        item = PREVIEWS.get(ticket)
        if not item or item[1] < time.time():
            return None
        return entry(item[0])['media_url']


def run_monitor(stop):
    # Checks only previously added items, never the entire M3U database.
    while not stop.wait(30):
        try:
            with connect() as c:
                due = [r[0] for r in c.execute('SELECT key FROM managed WHERE next_check <= ? ORDER BY next_check LIMIT 10', (time.time(),))]
            for key in due:
                if stop.is_set():
                    return
                try:
                    result = recheck(key)
                    print('MONITOR', result['status'], result['failures'], flush=True)
                except Exception as ex:
                    print('MONITOR falhou:', type(ex).__name__, flush=True)
        except Exception as ex:
            print('MONITOR erro:', type(ex).__name__, flush=True)


def content_states(keys):
    """Return existing contents, irrespective of the M3U source."""
    result = {}
    with connect() as c:
        rows = c.execute('SELECT key,kind,content_key,status,relative_path FROM managed').fetchall()
    for requested in keys:
        if not isinstance(requested, str) or len(requested)>300: continue
        found=next((r for r in rows if compatible(requested,r['content_key'])),None)
        if found:
            result[requested]={'key':found['key'],'status':found['status'],'path':found['relative_path']}
    return result


def delete_managed(key=None, series_title=None):
    """Only delete files explicitly owned by this Indexer; no arbitrary paths."""
    from indexer import norm
    if bool(key) == bool(series_title):
        raise ValueError('Informe uma chave ou uma série, não ambas')
    if key and (not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',key)):
        raise ValueError('Chave inválida')
    if series_title and (not isinstance(series_title,str) or not 2 <= len(series_title) <= 180):
        raise ValueError('Nome de série inválido')
    with LOCK:
        with connect() as c:
            rows=c.execute('SELECT * FROM managed').fetchall()
        selected=[r for r in rows if r['key']==key] if key else [r for r in rows if r['kind']=='episode' and norm(r['title'])==norm(series_title)]
        if not selected and key:
            raise ValueError('Item não gerenciado')
        removed=[]
        for r in selected:
            target=safe_dest(r['relative_path'])
            quarantine=(DATA/'quarantine'/(r['key']+'.strm')).resolve()
            if not quarantine.is_relative_to(DATA): raise ValueError('Quarentena inválida')
            # Remove precisely the files owned by this key, never entire folders.
            for path in (target,quarantine):
                if path.is_file():
                    # Do not delete a user-replaced STRM with another URL.
                    if f'/play/{r["key"]}' not in path.read_text(encoding='utf-8',errors='replace'):
                        raise ValueError('STRM modificado manualmente; exclusão cancelada')
            for path in (target,target.with_suffix('.nfo'),quarantine,quarantine.with_suffix('.nfo')):
                if path.is_file(): path.unlink()
            if r['kind']=='movie':
                for suffix in ('.jpg','.png','.webp'):
                    sidecar=target.with_name(target.stem+'-poster'+suffix)
                    if sidecar.is_file():sidecar.unlink()
            removed.append(r['key'])
        with connect() as c:
            for k in removed:
                c.execute('DELETE FROM managed WHERE key=?',(k,))
                c.execute('DELETE FROM validations WHERE entry_id NOT IN (SELECT entry_id FROM managed)')
                c.execute('DELETE FROM tmdb_matches WHERE key=?',(k,)) if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tmdb_matches'").fetchone() else None
            # Alert history deliberately preserved.
            if series_title:
                table=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='series_pending'").fetchone()
                if table: c.execute('DELETE FROM series_pending WHERE normalized=?',(norm(series_title),))
        if series_title:
            folder=(EXPORT_ROOT/'Series'/clean_name(series_title)).resolve()
            if folder.is_relative_to(EXPORT_ROOT) and folder.is_dir():
                # Remove only Indexer-created tvshow.nfo; keep user files and directories.
                nfo=folder/'tvshow.nfo'
                if nfo.is_file(): nfo.unlink()
                for suffix in ('.jpg','.png','.webp'):
                    poster=folder/('poster'+suffix)
                    if poster.is_file():poster.unlink()
        return {'ok':True,'removed':len(removed)}
