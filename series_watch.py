"""Séries monitoradas (como o Sonarr): episódios novos que aparecem nas listas viram STRM sozinhos.

Como funciona
- O administrador marca uma série como "monitorada" (inteira ou a partir de uma temporada).
- De tempos em tempos as listas cadastradas por link são baixadas de novo e o catálogo é reindexado.
- Sempre que o catálogo muda (por essa rotina ou por uma reindexação manual), cada série monitorada
  é comparada com o que já foi processado: episódios nunca vistos passam pela mesma validação da
  importação em lote (fonte testada antes de virar STRM) e entram no Jellyfin.
- Episódios novos sem fonte válida ficam pendentes e entram na nova tentativa diária que já existe.

Variáveis
  INDEXER_WATCH_REFRESH_HOURS  intervalo para baixar de novo as listas por link (padrão 6; 0 desliga)
"""
import os
import sqlite3
import threading
import time

import media_control as media
import series_bulk as bulk
from indexer import DB, norm

REFRESH_HOURS = float(os.environ.get('INDEXER_WATCH_REFRESH_HOURS', '6') or 0)
MAX_NEW_PER_SERIES = 200        # trava de segurança por verificação
WAKE = threading.Event()
STATE = {'running': False, 'last_check': 0.0, 'last_refresh': 0.0, 'message': ''}


def setup():
    with bulk.db_connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS series_watch(
            normalized TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL, from_season INTEGER,
            created_at REAL NOT NULL, last_check REAL, last_new REAL,
            added_total INTEGER NOT NULL DEFAULT 0, last_message TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(normalized, source)
        );
        CREATE TABLE IF NOT EXISTS series_watch_meta(k TEXT PRIMARY KEY, v TEXT);
        ''')


def _meta(c, k, default=''):
    row = c.execute('SELECT v FROM series_watch_meta WHERE k=?', (k,)).fetchone()
    return row['v'] if row else default


def _set_meta(c, k, v):
    c.execute('INSERT INTO series_watch_meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, str(v)))


def _valid(title, source):
    if not isinstance(title, str) or not 2 <= len(title.strip()) <= 180:
        raise ValueError('Título inválido')
    if not isinstance(source, str) or len(source) > 200:
        raise ValueError('Origem inválida')
    normalized = norm(title.strip())
    if not normalized:
        raise ValueError('Título não identificado')
    return normalized


def watch(title, source='', season=None, on=True):
    normalized = _valid(title, source)
    with bulk.db_connect() as c:
        if not on:
            c.execute('DELETE FROM series_watch WHERE normalized=? AND source=?', (normalized, source))
            return {'watching': False}
        if season not in (None, ''):
            season = int(season)
            if not 0 <= season <= 999:
                raise ValueError('Temporada inválida')
        else:
            season = None
        c.execute('''INSERT INTO series_watch(normalized,source,title,from_season,created_at)
            VALUES(?,?,?,?,?) ON CONFLICT(normalized,source) DO UPDATE SET title=excluded.title, from_season=excluded.from_season''',
                  (normalized, source, title.strip(), season, time.time()))
    WAKE.set()          # verifica logo
    return {'watching': True, 'from_season': season}


def status(title, source=''):
    normalized = _valid(title, source)
    with bulk.db_connect() as c:
        row = c.execute('SELECT * FROM series_watch WHERE normalized=? AND source=?', (normalized, source)).fetchone()
    return {'watching': bool(row), **(dict(row) if row else {})}


def listing():
    with bulk.db_connect() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM series_watch ORDER BY title COLLATE NOCASE')]
    return {'items': rows, 'state': dict(STATE), 'refresh_hours': REFRESH_HOURS}


def check_now():
    WAKE.set()
    return {'started': True}


# ------------------------------------------------------------------ verificação
def _check_one(row):
    """Processa os episódios dessa série que ainda não passaram pelo indexador. Retorna quantos entraram."""
    try:
        _, grouped = bulk._candidates(row['title'], row['source'], None)
    except ValueError as exc:
        return 0, str(exc)[:120]
    if row['from_season'] is not None:
        grouped = {k: v for k, v in grouped.items() if k[0] >= row['from_season']}
    with bulk.db_connect() as c:
        known = {(r['season'], r['episode']) for r in c.execute(
            'SELECT season,episode FROM series_pending WHERE normalized=? AND source=?', (row['normalized'], row['source']))}
    new = sorted(k for k in grouped if k not in known)[:MAX_NEW_PER_SERIES]
    added, waiting = [], 0
    for season, episode in new:
        with bulk.PROCESS_LOCK:
            result = bulk._resolve_one(row['title'], row['normalized'], row['source'], season, episode, grouped[(season, episode)])
        if result == 'added':
            added.append((season, episode))
        elif result == 'pending':
            waiting += 1
    if added:
        codes = ', '.join(f'S{s:02}E{e:02}' for s, e in added[:6]) + ('…' if len(added) > 6 else '')
        msg = f'{len(added)} episódio(s) novo(s) adicionado(s): {codes}'
        with media.connect() as c:
            c.execute('INSERT INTO alerts (key,title,event,message,created_at) VALUES (?,?,?,?,?)',
                      ('', row['title'], 'new_episode', msg, media.utc_now()))
        media.send_notification('new_episode', row['title'], msg)
        _ask_library_refresh()
    elif waiting:
        msg = f'{waiting} episódio(s) novo(s) sem fonte válida; nova tentativa diária'
    elif new:
        msg = 'Episódios novos já estavam no Jellyfin'
    else:
        msg = 'Sem episódios novos'
    return len(added), msg


def _ask_library_refresh():
    try:
        import jellyfin_bridge
        jellyfin_bridge._refresh['pending'] = True
        jellyfin_bridge.WAKE.set()
    except Exception:
        pass


def check_all():
    with bulk.db_connect() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM series_watch')]
    total = 0
    for row in rows:
        try:
            n, msg = _check_one(row)
        except (sqlite3.Error, OSError) as exc:
            n, msg = 0, 'Erro: ' + type(exc).__name__
        total += n
        with bulk.db_connect() as c:
            c.execute('''UPDATE series_watch SET last_check=?, last_message=?, added_total=added_total+?,
                last_new=CASE WHEN ?>0 THEN ? ELSE last_new END WHERE normalized=? AND source=?''',
                      (time.time(), msg, n, n, time.time(), row['normalized'], row['source']))
    return total


def _refresh_lists():
    """Baixa de novo as listas cadastradas por link e reindexa (como o botão "Atualizar")."""
    import playlist_admin as playlists
    names = [f['name'] for f in playlists.status().get('files', []) if f.get('github')]
    changed = False
    for name in names:
        try:
            playlists.refresh(name)
            changed = True
        except Exception as exc:
            print(f'SERIES_WATCH: lista {name} não atualizada ({str(exc)[:80]})', flush=True)
    if not changed:
        return
    try:
        playlists.start_import()
    except ValueError:
        pass                                    # já havia uma reindexação rodando
    deadline = time.time() + 3600
    while playlists.JOB.get('state') == 'running' and time.time() < deadline:
        time.sleep(5)


def _catalog_mtime():
    try:
        return os.path.getmtime(DB)
    except OSError:
        return 0.0


def loop(stop):
    print(f'SERIES_WATCH: monitor iniciado (listas por link a cada {REFRESH_HOURS:g} h)', flush=True)
    while not stop.is_set():
        try:
            with bulk.db_connect() as c:
                has_watch = c.execute('SELECT 1 FROM series_watch LIMIT 1').fetchone() is not None
                last_refresh = float(_meta(c, 'last_refresh', '0') or 0)
                last_mtime = float(_meta(c, 'catalog_mtime', '0') or 0)
                forced = _meta(c, 'force', '') == '1'
            if not has_watch:
                WAKE.clear()                    # nada monitorado: não fica girando
            else:
                STATE['last_refresh'] = last_refresh
                if REFRESH_HOURS > 0 and time.time() - last_refresh >= REFRESH_HOURS * 3600:
                    STATE.update(running=True, message='Atualizando listas…')
                    _refresh_lists()
                    with bulk.db_connect() as c:
                        _set_meta(c, 'last_refresh', time.time())
                    STATE['last_refresh'] = time.time()
                mtime = _catalog_mtime()
                if mtime != last_mtime or WAKE.is_set() or forced:
                    WAKE.clear()
                    STATE.update(running=True, message='Procurando episódios novos…')
                    n = check_all()
                    with bulk.db_connect() as c:
                        _set_meta(c, 'catalog_mtime', mtime)
                        _set_meta(c, 'force', '')
                    STATE.update(last_check=time.time(), message=f'{n} episódio(s) novo(s) na última verificação')
                    if n:
                        print(f'SERIES_WATCH: {n} episódio(s) novo(s) adicionado(s)', flush=True)
        except Exception as exc:
            print('SERIES_WATCH:', type(exc).__name__, str(exc)[:120], flush=True)
        finally:
            STATE['running'] = False
        WAKE.wait(60)
