"""Limpeza semanal de links mortos.

Toda semana, de madrugada, testa uma parte dos links das listas (filmes, episódios e canais de TV) e
anota os que estão fora do ar. Um link só é dado como morto depois de falhar em DUAS verificações
seguidas; erro passageiro (429, 5xx, tempo esgotado) não conta. Os links mortos são ignorados em toda
reindexação (catálogo e aba Canais) — os arquivos M3U não são alterados, porque as listas por link
são baixadas de novo e trariam as linhas de volta; assim o efeito vale para qualquer lista e dá para
desfazer pelo painel.

Não testa: itens da aba Gerenciados (o monitor diário já cuida deles, inclusive trocando a fonte) e
endereços liberados em INDEXER_TRUSTED_HOSTS (ex.: Dispatcharr na rede local).

Variáveis
  INDEXER_PRUNE_DAY    dia da semana (0=segunda … 6=domingo; padrão 6) · "off" desliga
  INDEXER_PRUNE_HOUR   hora de início (padrão 3)
  INDEXER_PRUNE_MAX    links testados por rodada (padrão 20000)
  INDEXER_PRUNE_HOURS  duração máxima da rodada em horas (padrão 4)
"""
import hashlib
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

DATA = Path(os.environ.get('INDEXER_STATE_DIR', '/app/data'))
DB = DATA / 'link_health.sqlite'
_day = os.environ.get('INDEXER_PRUNE_DAY', '6').strip().lower()
DAY = None if _day in ('off', 'no', 'false', '-1', '') else max(0, min(6, int(_day)))
HOUR = max(0, min(23, int(os.environ.get('INDEXER_PRUNE_HOUR', '3'))))
MAX_PER_RUN = max(100, min(200000, int(os.environ.get('INDEXER_PRUNE_MAX', '20000'))))
MAX_HOURS = max(0.5, min(12., float(os.environ.get('INDEXER_PRUNE_HOURS', '4'))))
WORKERS = 8
PER_HOST = 2                      # conexões simultâneas por servidor (não incomodar provedores)
TRANSIENT = re.compile(r'HTTP (429|5\d\d)|timed out|tempo|Connection (reset|aborted|refused)|'
                       r'Remote end closed|temporar|Não foi possível resolver', re.I)
STATE = {'running': False, 'started_at': 0.0, 'checked': 0, 'total': 0, 'new_dead': 0, 'message': ''}
_lock = threading.Lock()
_dead_cache = {'mtime': None, 'urls': frozenset()}


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute('''CREATE TABLE IF NOT EXISTS links(
        h TEXT PRIMARY KEY, url TEXT NOT NULL, kind TEXT, source TEXT, title TEXT,
        fails INTEGER NOT NULL DEFAULT 0, dead INTEGER NOT NULL DEFAULT 0,
        last_check REAL, last_error TEXT, dead_at REAL)''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_links_dead ON links(dead)')
    c.execute('CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)')
    return c


def _h(url):
    return hashlib.sha1(url.encode()).hexdigest()


def dead_urls():
    """Endereços excluídos (usado pela reindexação e pela aba Canais)."""
    try:
        mtime = DB.stat().st_mtime
    except OSError:
        return frozenset()
    if _dead_cache['mtime'] != mtime:
        with connect() as c:
            _dead_cache['urls'] = frozenset(r[0] for r in c.execute('SELECT url FROM links WHERE dead=1'))
        _dead_cache['mtime'] = mtime
    return _dead_cache['urls']


def _meta(c, k, default=''):
    r = c.execute('SELECT v FROM meta WHERE k=?', (k,)).fetchone()
    return r[0] if r else default


def _set_meta(c, k, v):
    c.execute('INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, str(v)))


def status():
    with connect() as c:
        dead = c.execute('SELECT COUNT(*) FROM links WHERE dead=1').fetchone()[0]
        suspect = c.execute('SELECT COUNT(*) FROM links WHERE dead=0 AND fails>0').fetchone()[0]
        checked = c.execute('SELECT COUNT(*) FROM links WHERE last_check IS NOT NULL').fetchone()[0]
        last = float(_meta(c, 'last_run', '0') or 0)
        result = _meta(c, 'last_result')
        recent = [dict(r) for r in c.execute(
            'SELECT title, kind, source, last_error, dead_at FROM links WHERE dead=1 ORDER BY dead_at DESC LIMIT 15')]
    days = ['segunda', 'terça', 'quarta', 'quinta', 'sexta', 'sábado', 'domingo']
    return {'schedule': None if DAY is None else f'{days[DAY]} às {HOUR:02d}h', 'max_per_run': MAX_PER_RUN,
            'dead': dead, 'suspect': suspect, 'checked': checked, 'last_run': last, 'last_result': result,
            'state': dict(STATE), 'recent': recent}


def remove_manual(url, kind='movie', source='', title=''):
    """Retira um link à mão (ex.: na aba Revisão o filme não abriu). Some das próximas indexações e
    pode ser desfeito em "Restaurar" da limpeza de links."""
    now = time.time()
    with connect() as c:
        c.execute('''INSERT INTO links(h,url,kind,source,title,fails,dead,last_check,last_error,dead_at)
                     VALUES(?,?,?,?,?,1,1,?,'Retirado manualmente',?)
                     ON CONFLICT(h) DO UPDATE SET dead=1, last_check=excluded.last_check,
                        last_error='Retirado manualmente', dead_at=excluded.dead_at''',
                  (_h(url), url, kind, source, title, now, now))
    _dead_cache['mtime'] = None
    return {'ok': True}


def is_protected(url):
    """True se o link é usado por um STRM gerenciado ou é de um host liberado."""
    return url in _protected()


def restore(url=None):
    """Desfaz a exclusão (de um link ou de todos). Os links voltam na próxima reindexação."""
    with connect() as c:
        if url:
            c.execute('UPDATE links SET dead=0, fails=0 WHERE url=?', (url,))
        else:
            c.execute('UPDATE links SET dead=0, fails=0 WHERE dead=1')
    _reindex()
    return {'ok': True}


# ------------------------------------------------------------------ candidatos
def _protected():
    """Links que a limpeza não testa: gerenciados (monitor próprio) e hosts liberados (rede local)."""
    urls = set()
    try:
        import media_control as media
        from indexer import DB as CATALOG
        with media.connect() as mc:
            ids = [r[0] for r in mc.execute('SELECT entry_id FROM managed')]
        if ids:
            with sqlite3.connect(f'file:{CATALOG}?mode=ro', uri=True, timeout=20) as cat:
                for i in range(0, len(ids), 500):
                    part = ids[i:i + 500]
                    urls |= {r[0] for r in cat.execute(
                        'SELECT media_url FROM entries WHERE id IN (%s)' % ','.join('?' * len(part)), part)}
    except Exception:
        pass
    try:
        import epg
        hosts = {h.split(':')[0] for h in epg.trusted_hosts()}
    except Exception:
        hosts = set()
    return urls, hosts


def _candidates(limit):
    """Primeiro os que já falharam uma vez (confirmação), depois os nunca testados, depois os mais antigos."""
    from indexer import DB as CATALOG
    import channels
    protected, trusted = _protected()
    pool = {}
    with sqlite3.connect(f'file:{CATALOG}?mode=ro', uri=True, timeout=30) as cat:
        for url, kind, source, title in cat.execute(
                "SELECT media_url, kind, source, title FROM entries WHERE kind IN ('movie','episode')"):
            pool[url] = ('vod', source, title, '', '')
    for ch in channels.index()[0]:
        pool.setdefault(ch['url'], ('live', ch['source'], ch['name'], ch.get('ua') or '', ch.get('referer') or ''))
    for url in list(pool):
        if url in protected or (urlsplit(url).hostname or '') in trusted:
            pool.pop(url)
    with connect() as c:
        known = {r['h']: (r['fails'], r['last_check'] or 0, r['dead']) for r in c.execute('SELECT h, fails, last_check, dead FROM links')}
    ranked = []
    for url, (kind, source, title, ua, ref) in pool.items():
        fails, last, dead = known.get(_h(url), (0, 0, 0))
        if dead:
            continue
        ranked.append(((0 if fails else 1, last), url, kind, source, title, ua, ref))
    ranked.sort(key=lambda x: x[0])
    return [r[1:] for r in ranked[:limit]], len(pool)


# ------------------------------------------------------------------ teste
def _check(url, kind, ua, ref):
    if kind == 'live':
        import channels
        ok, detail, _ = channels.probe(url, ua or None, ref or None)
    else:
        import media_control as media
        ok, detail = media.validate_media(url)
    return ok, (detail or '')[:200]


def run(force=False):
    if not _lock.acquire(blocking=False):
        raise ValueError('A limpeza já está rodando')
    try:
        STATE.update(running=True, started_at=time.time(), checked=0, total=0, new_dead=0, message='Montando a lista de links…')
        items, pool_size = _candidates(MAX_PER_RUN)
        STATE.update(total=len(items), message=f'Testando {len(items)} de {pool_size} links…')
        deadline = time.time() + MAX_HOURS * 3600
        host_locks = defaultdict(lambda: threading.Semaphore(PER_HOST))
        results = []

        def work(item):
            url, kind, source, title, ua, ref = item
            if time.time() > deadline:
                return None
            with host_locks[urlsplit(url).hostname or '']:
                try:
                    ok, detail = _check(url, kind, ua, ref)
                except Exception as exc:
                    ok, detail = False, type(exc).__name__
            STATE['checked'] += 1
            return item, ok, detail

        with ThreadPoolExecutor(WORKERS) as pool:
            for res in pool.map(work, items):
                if res:
                    results.append(res)
        new_dead = 0
        now = time.time()
        with connect() as c:
            for (url, kind, source, title, _, _), ok, detail in results:
                h = _h(url)
                row = c.execute('SELECT fails FROM links WHERE h=?', (h,)).fetchone()
                fails = row['fails'] if row else 0
                if ok:
                    fails, dead = 0, 0
                elif TRANSIENT.search(detail):
                    dead = 0                                    # oscilação: não conta
                else:
                    fails += 1
                    dead = 1 if fails >= 2 else 0
                    new_dead += dead
                c.execute('''INSERT INTO links(h,url,kind,source,title,fails,dead,last_check,last_error,dead_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(h) DO UPDATE SET fails=excluded.fails, dead=excluded.dead,
                    last_check=excluded.last_check, last_error=excluded.last_error, source=excluded.source,
                    title=excluded.title, dead_at=CASE WHEN excluded.dead=1 THEN excluded.dead_at ELSE links.dead_at END''',
                          (h, url, kind, source, (title or '')[:200], fails, dead, now, None if ok else detail,
                           now if dead else None))
            msg = f'{len(results)} links testados · {new_dead} excluídos nesta rodada'
            _set_meta(c, 'last_run', now)
            _set_meta(c, 'last_result', msg)
        STATE.update(new_dead=new_dead, message=msg)
        print('LINK_PRUNE:', msg, flush=True)
        if new_dead:
            _reindex()
        return {'ok': True, 'message': msg}
    finally:
        STATE['running'] = False
        _lock.release()


def _reindex():
    try:
        import playlist_admin
        playlist_admin.start_import()
    except Exception:
        pass                                            # já havia uma reindexação rodando


def start_now():
    if STATE['running']:
        raise ValueError('A limpeza já está rodando')
    threading.Thread(target=lambda: _safe_run(), daemon=True, name='link-prune').start()
    return {'started': True}


def _safe_run():
    try:
        run()
    except Exception as exc:
        STATE['message'] = 'Erro: ' + str(exc)[:150]
        print('LINK_PRUNE: erro', type(exc).__name__, str(exc)[:150], flush=True)


def loop(stop):
    if DAY is None:
        print('LINK_PRUNE: limpeza semanal desligada', flush=True)
        return
    print(f'LINK_PRUNE: limpeza semanal agendada (dia {DAY}, {HOUR:02d}h)', flush=True)
    while not stop.is_set():
        try:
            now = datetime.now()
            with connect() as c:
                last = float(_meta(c, 'last_run', '0') or 0)
            if now.weekday() == DAY and now.hour == HOUR and time.time() - last > 5 * 86400 and not STATE['running']:
                _safe_run()
        except Exception as exc:
            print('LINK_PRUNE:', type(exc).__name__, str(exc)[:120], flush=True)
        stop.wait(600)
